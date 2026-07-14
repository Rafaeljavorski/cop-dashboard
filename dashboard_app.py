import os
import secrets
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

import psycopg
import requests
from psycopg.rows import dict_row
from flask import Flask, jsonify, render_template, request, session, redirect, url_for, Response
from werkzeug.security import generate_password_hash, check_password_hash

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("Configure DATABASE_URL no Railway.")

# Necessário pro Flask assinar o cookie de sessão (login). Gere um valor
# aleatório uma vez e configure como variável de ambiente SECRET_KEY no
# Railway — se não tiver, cai num valor aleatório gerado a cada reinício,
# o que faria todo mundo deslogar sempre que o serviço reiniciasse.
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)

# Precisa do MESMO token do bot do Telegram, pra conseguir mandar mensagem
# direto (sem precisar do bot_cop_telegram.py rodando pra isso).
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Configure TELEGRAM_BOT_TOKEN (o mesmo token do bot) no Railway.")

# Senha simples de administrador, só pra proteger a tela de definir/trocar
# senha dos atendentes. Não é por atendente — é uma senha só, de quem
# administra o sistema.
ADMIN_SECRET = os.getenv("ADMIN_SECRET")

# MESMA variável ADM_IDS que o bot já usa (Settings → Variables do serviço
# do bot, copia o valor) — assim quem é admin lá também é admin aqui, sem
# duplicar cadastro. Formato: IDs do Telegram separados por vírgula.
ADM_IDS = [
    int(x.strip())
    for x in os.getenv("ADM_IDS", "").split(",")
    if x.strip()
]


def eh_admin(user_id):
    return user_id in ADM_IDS


app = Flask(__name__)
app.secret_key = SECRET_KEY

# O servidor (Railway) roda em UTC, não no horário de Brasília. O bot
# (bot_cop_telegram.py) grava os timestamps já corrigidos pra Brasília
# (naive, sem indicação de fuso) — esse painel precisa comparar "agora"
# usando o MESMO ajuste, senão as contas de minutos ficam 3h erradas.
TZ_BRASIL = ZoneInfo("America/Sao_Paulo")


def agora():
    return datetime.now(TZ_BRASIL).replace(tzinfo=None)


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def minutos(dt_iso):
    if not dt_iso:
        return 0
    try:
        dt = datetime.fromisoformat(str(dt_iso))
        return int((agora() - dt).total_seconds() // 60)
    except Exception:
        return 0


def fetchall(sql, params=()):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def fetchone(sql, params=()):
    rows = fetchall(sql, params)
    return rows[0] if rows else None


def executar(sql, params=()):
    """Pra INSERT/UPDATE que não precisam devolver linhas."""
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


def executar_retornando(sql, params=()):
    """Pra INSERT/UPDATE com RETURNING — devolve as linhas afetadas."""
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            linhas = cur.fetchall()
        conn.commit()
        return linhas


def migrar_schema():
    """Roda uma vez na subida do serviço: adiciona a coluna de senha se
    ainda não existir. Não depende do bot_cop_telegram.py ter sido
    redeployado — esse serviço cuida da própria coluna que precisa."""
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE atendentes ADD COLUMN IF NOT EXISTS senha_hash TEXT")
        conn.commit()


def telegram_api(metodo, **params):
    """
    Chama a API HTTP do Telegram diretamente (sem precisar da biblioteca
    python-telegram-bot) — usa o MESMO token do bot, então mensagens
    enviadas por aqui chegam pro técnico e pro tópico do grupo exatamente
    como se o bot tivesse mandado.
    """
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{metodo}",
        json=params,
        timeout=10,
    )
    dados = resp.json()
    if not dados.get("ok"):
        raise RuntimeError(f"Telegram API ({metodo}) falhou: {dados.get('description')}")
    return dados["result"]


# Roda na importação do módulo — funciona tanto rodando direto
# (python dashboard_app.py) quanto via gunicorn (que nunca executa o bloco
# "if __name__ == '__main__'" lá embaixo).
migrar_schema()


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("atendente_id"):
            if request.path.startswith("/api/"):
                return jsonify({"erro": "não autenticado"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    erro = None
    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        senha = request.form.get("senha") or ""
        atendente = fetchone(
            "SELECT user_id, nome, senha_hash FROM atendentes WHERE nome=%s AND ativo=TRUE",
            (nome,),
        )
        if not atendente or not atendente.get("senha_hash") or not check_password_hash(atendente["senha_hash"], senha):
            erro = "Nome ou senha incorretos."
        else:
            session["atendente_id"] = atendente["user_id"]
            session["atendente_nome"] = atendente["nome"]
            return redirect(url_for("inbox"))
    return render_template("login.html", erro=erro)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin/senha", methods=["GET", "POST"])
def admin_senha():
    """
    Tela simples (protegida por uma senha de administrador só, separada da
    senha de cada atendente) pra definir ou trocar a senha de login de
    qualquer atendente cadastrado.
    """
    if not ADMIN_SECRET:
        return "Configure a variável ADMIN_SECRET no Railway pra habilitar esta tela.", 503

    autenticado = session.get("admin_ok")
    mensagem = None

    if request.method == "POST":
        if not autenticado:
            if request.form.get("admin_secret") == ADMIN_SECRET:
                session["admin_ok"] = True
                autenticado = True
            else:
                mensagem = "Senha de administrador incorreta."
        else:
            nome = (request.form.get("nome") or "").strip()
            nova_senha = request.form.get("nova_senha") or ""
            if len(nova_senha) < 4:
                mensagem = "A senha precisa ter pelo menos 4 caracteres."
            else:
                linhas_afetadas = executar_retornando(
                    "UPDATE atendentes SET senha_hash=%s WHERE nome=%s RETURNING user_id",
                    (generate_password_hash(nova_senha), nome),
                )
                mensagem = f"Senha de {nome} atualizada." if linhas_afetadas else f"Não achei nenhum atendente chamado '{nome}'."

    atendentes = fetchall("SELECT nome FROM atendentes WHERE ativo=TRUE ORDER BY nome") if autenticado else []
    return render_template("admin_senha.html", autenticado=autenticado, mensagem=mensagem, atendentes=atendentes)


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/inbox")
@login_required
def inbox():
    return render_template("inbox.html", atendente_nome=session.get("atendente_nome"))


@app.route("/api/inbox/tickets")
@login_required
def api_inbox_tickets():
    meu_id = session["atendente_id"]
    sou_admin = eh_admin(meu_id)

    if sou_admin:
        # Admin enxerga o trabalho de todo mundo, não só o próprio — mesmo
        # princípio de "Gestão ADM" no bot do Telegram.
        ativos = fetchall("""
            SELECT protocolo, user_name, categoria, subcategoria, contrato, created_at, assumed_at, last_message_at, atendente_nome
            FROM tickets
            WHERE status='em_atendimento'
            ORDER BY last_message_at DESC NULLS LAST, assumed_at DESC
        """)
        finalizados = fetchall("""
            SELECT protocolo, user_name, categoria, subcategoria, contrato, closed_at, atendente_nome
            FROM tickets
            WHERE status='finalizado'
            ORDER BY closed_at DESC
            LIMIT 50
        """)
    else:
        ativos = fetchall("""
            SELECT protocolo, user_name, categoria, subcategoria, contrato, created_at, assumed_at, last_message_at, atendente_nome
            FROM tickets
            WHERE status='em_atendimento' AND atendente_id=%s
            ORDER BY last_message_at DESC NULLS LAST, assumed_at DESC
        """, (meu_id,))
        finalizados = fetchall("""
            SELECT protocolo, user_name, categoria, subcategoria, contrato, closed_at, atendente_nome
            FROM tickets
            WHERE status='finalizado' AND atendente_id=%s
            ORDER BY closed_at DESC
            LIMIT 50
        """, (meu_id,))

    aguardando = fetchall("""
        SELECT protocolo, user_name, categoria, subcategoria, contrato, created_at
        FROM tickets
        WHERE status='aguardando'
        ORDER BY id ASC
    """)

    ativos_out = []
    for r in ativos:
        x = dict(r)
        x["espera_min"] = minutos(x.get("last_message_at") or x.get("assumed_at"))
        ativos_out.append(x)

    aguardando_out = []
    for r in aguardando:
        x = dict(r)
        x["espera_min"] = minutos(x.get("created_at"))
        aguardando_out.append(x)

    return jsonify({
        "ativos": ativos_out,
        "aguardando": aguardando_out,
        "finalizados": [dict(r) for r in finalizados],
        "sou_admin": sou_admin,
        "atualizado_em": agora().strftime("%H:%M:%S"),
    })


@app.route("/api/inbox/ticket/<protocolo>/mensagens")
@login_required
def api_inbox_mensagens(protocolo):
    meu_id = session["atendente_id"]
    ticket = fetchone("SELECT * FROM tickets WHERE protocolo=%s", (protocolo,))
    if not ticket:
        return jsonify({"erro": "chamado não encontrado"}), 404
    if ticket["status"] == "em_atendimento" and ticket["atendente_id"] != meu_id and not eh_admin(meu_id):
        return jsonify({"erro": "esse chamado é de outro atendente"}), 403

    mensagens = fetchall("""
        SELECT sender_name, sender_role, message_type, text, file_id, latitude, longitude, created_at
        FROM messages
        WHERE protocolo=%s
        ORDER BY id ASC
    """, (protocolo,))

    return jsonify({
        "ticket": dict(ticket),
        "mensagens": [dict(m) for m in mensagens],
    })


@app.route("/api/inbox/arquivo/<file_id>")
@login_required
def api_inbox_arquivo(file_id):
    """
    Repassa uma foto/arquivo do Telegram pro navegador, sem nunca expor o
    token do bot pro lado do cliente (o link direto do Telegram inclui o
    token na URL — se colocássemos isso num <img src>, qualquer atendente
    veria o token no código-fonte da página).
    """
    meu_id = session["atendente_id"]

    msg = fetchone("SELECT protocolo FROM messages WHERE file_id=%s", (file_id,))
    if not msg:
        return "Arquivo não encontrado.", 404

    ticket = fetchone("SELECT atendente_id, status FROM tickets WHERE protocolo=%s", (msg["protocolo"],))
    if ticket and ticket["status"] == "em_atendimento" and ticket["atendente_id"] != meu_id and not eh_admin(meu_id):
        return "Sem permissão pra ver esse arquivo.", 403

    try:
        info = telegram_api("getFile", file_id=file_id)
        file_path = info["file_path"]
    except Exception:
        return "Não consegui localizar esse arquivo no Telegram (pode ter expirado).", 404

    try:
        resp = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}", timeout=15)
        resp.raise_for_status()
    except Exception:
        return "Não consegui baixar o arquivo do Telegram.", 502

    content_type = resp.headers.get("Content-Type", "application/octet-stream")
    return Response(resp.content, content_type=content_type, headers={"Cache-Control": "private, max-age=3600"})


@app.route("/api/inbox/ticket/<protocolo>/enviar", methods=["POST"])
@login_required
def api_inbox_enviar(protocolo):
    meu_id = session["atendente_id"]
    meu_nome = session["atendente_nome"]
    texto = ((request.json or {}).get("texto") or "").strip()
    if not texto:
        return jsonify({"erro": "mensagem vazia"}), 400

    ticket = fetchone("SELECT * FROM tickets WHERE protocolo=%s", (protocolo,))
    if not ticket:
        return jsonify({"erro": "chamado não encontrado"}), 404
    if ticket["status"] != "em_atendimento":
        return jsonify({"erro": "esse chamado não está em atendimento"}), 403
    if ticket["atendente_id"] != meu_id and not eh_admin(meu_id):
        return jsonify({"erro": "você não é o responsável por esse chamado"}), 403

    cabecalho = f"📩 {protocolo} - COP {meu_nome}"

    try:
        telegram_api("sendMessage", chat_id=ticket["user_id"], text=f"{cabecalho}\n\n{texto}")
    except Exception as e:
        return jsonify({"erro": f"não consegui entregar ao técnico: {e}"}), 502

    # Espelha no tópico do grupo (se existir) — é isso que mantém o painel
    # web e o Telegram em paralelo, sincronizados.
    if ticket.get("message_thread_id") and ticket.get("grupo_atendente"):
        try:
            telegram_api(
                "sendMessage",
                chat_id=ticket["grupo_atendente"],
                message_thread_id=ticket["message_thread_id"],
                text=f"💻 (via painel web) {texto}",
            )
        except Exception:
            pass  # não é crítico: a mensagem já chegou pro técnico de qualquer forma

    agora_str = agora().isoformat(timespec="seconds")
    executar(
        """
        INSERT INTO messages (protocolo, sender_id, sender_name, sender_role, message_type, text, created_at)
        VALUES (%s, %s, %s, 'atendente', 'mensagem', %s, %s)
        """,
        (protocolo, meu_id, meu_nome, texto, agora_str),
    )
    executar("UPDATE tickets SET last_message_at=%s WHERE protocolo=%s", (agora_str, protocolo))

    return jsonify({"ok": True})


def reenviar_historico_para_topico(protocolo, thread_id, grupo_id):
    """
    Mesma lógica do bot do Telegram (reenviar_historico_para_topico): quando
    um tópico é criado agora (porque o chamado foi assumido pelo painel
    web), ele nasce vazio -- sem isso, tudo que o técnico já tinha mandado
    ANTES de alguém assumir (fotos, localização, texto da triagem inicial)
    fica invisível pra quem olhar direto no Telegram.
    """
    if not thread_id:
        return

    ticket = fetchone("SELECT historico_enviado FROM tickets WHERE protocolo=%s", (protocolo,))
    if ticket and ticket.get("historico_enviado"):
        return

    msgs = fetchall(
        "SELECT * FROM messages WHERE protocolo=%s AND sender_role='tecnico' ORDER BY id ASC",
        (protocolo,),
    )

    if not msgs:
        executar("UPDATE tickets SET historico_enviado=1 WHERE protocolo=%s", (protocolo,))
        return

    try:
        telegram_api(
            "sendMessage", chat_id=grupo_id, message_thread_id=thread_id,
            text="📎 *Histórico/evidências enviados pelo técnico:*", parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning("Falha ao enviar cabeçalho do histórico de %s: %s", protocolo, e)

    for m in msgs:
        try:
            legenda = f"📩 {protocolo} - {m['sender_name']}"
            if m.get("text"):
                legenda += f"\n\n{m['text']}"

            tipo = m.get("message_type")
            if tipo == "text":
                telegram_api("sendMessage", chat_id=grupo_id, message_thread_id=thread_id, text=legenda)
            elif tipo == "photo":
                telegram_api("sendPhoto", chat_id=grupo_id, message_thread_id=thread_id, photo=m["file_id"], caption=legenda)
            elif tipo == "document":
                telegram_api("sendDocument", chat_id=grupo_id, message_thread_id=thread_id, document=m["file_id"], caption=legenda)
            elif tipo == "video":
                telegram_api("sendVideo", chat_id=grupo_id, message_thread_id=thread_id, video=m["file_id"], caption=legenda)
            elif tipo == "voice":
                telegram_api("sendVoice", chat_id=grupo_id, message_thread_id=thread_id, voice=m["file_id"], caption=legenda)
            elif tipo == "location":
                telegram_api("sendLocation", chat_id=grupo_id, message_thread_id=thread_id, latitude=m["latitude"], longitude=m["longitude"])
                telegram_api("sendMessage", chat_id=grupo_id, message_thread_id=thread_id, text=legenda)
        except Exception as e:
            logger.warning("Falha ao reenviar item do histórico de %s: %s", protocolo, e)

    executar("UPDATE tickets SET historico_enviado=1 WHERE protocolo=%s", (protocolo,))


@app.route("/api/inbox/ticket/<protocolo>/assumir", methods=["POST"])
@login_required
def api_inbox_assumir(protocolo):
    meu_id = session["atendente_id"]
    meu_nome = session["atendente_nome"]

    atendente = fetchone("SELECT grupo_id FROM atendentes WHERE user_id=%s AND ativo=TRUE", (meu_id,))
    if not atendente:
        return jsonify({"erro": "seu cadastro de atendente não está ativo"}), 403
    grupo_id = atendente["grupo_id"]

    # Reivindica de forma atômica — mesmo princípio usado no bot: só um
    # processo consegue vencer a corrida se dois cliques (bot do Telegram +
    # painel web, ou dois atendentes no painel web) chegarem quase juntos.
    linhas = executar_retornando(
        "UPDATE tickets SET status='em_atendimento' WHERE protocolo=%s AND status='aguardando' RETURNING id",
        (protocolo,),
    )
    if not linhas:
        return jsonify({"erro": "esse chamado já foi assumido por outra pessoa"}), 409

    ticket = fetchone("SELECT * FROM tickets WHERE protocolo=%s", (protocolo,))

    thread_id = ticket.get("message_thread_id")
    if not thread_id or ticket.get("grupo_atendente") != grupo_id:
        try:
            topico = telegram_api(
                "createForumTopic",
                chat_id=grupo_id,
                name=f"🔵 {protocolo} - {ticket['user_name'][:18]} - {ticket['categoria']}",
            )
            thread_id = topico["message_thread_id"]
        except Exception:
            # Não trava o atendimento por causa disso: o painel web segue
            # funcionando mesmo sem espelho no Telegram, só fica sem tópico
            # até alguém resolver (ex.: bot sem permissão no grupo).
            thread_id = None

    agora_str = agora().isoformat(timespec="seconds")
    executar(
        """
        UPDATE tickets
        SET atendente_id=%s, atendente_nome=%s, assumed_at=%s, last_message_at=%s,
            grupo_atendente=%s, message_thread_id=COALESCE(%s, message_thread_id)
        WHERE protocolo=%s
        """,
        (meu_id, meu_nome, agora_str, agora_str, grupo_id, thread_id, protocolo),
    )

    # Replica pro tópico novo o que o técnico já tinha mandado antes de
    # alguém assumir -- sem isso, o tópico nasce vazio no lado do Telegram,
    # mesmo com o painel web mostrando a conversa completa.
    reenviar_historico_para_topico(protocolo, thread_id, grupo_id)

    try:
        telegram_api(
            "sendMessage",
            chat_id=ticket["user_id"],
            text=f"🔷 CIP Telecom\n\nSeu atendimento foi iniciado por: {meu_nome}\n🎫 Protocolo: {protocolo}",
        )
    except Exception:
        pass

    return jsonify({"ok": True})


@app.route("/api/inbox/ticket/<protocolo>/finalizar", methods=["POST"])
@login_required
def api_inbox_finalizar(protocolo):
    meu_id = session["atendente_id"]

    ticket = fetchone("SELECT * FROM tickets WHERE protocolo=%s", (protocolo,))
    if not ticket:
        return jsonify({"erro": "chamado não encontrado"}), 404
    if ticket["atendente_id"] != meu_id and not eh_admin(meu_id):
        return jsonify({"erro": "você não é o responsável por esse chamado"}), 403

    linhas = executar_retornando(
        "UPDATE tickets SET status='finalizado', closed_at=%s WHERE protocolo=%s AND status='em_atendimento' RETURNING id",
        (agora().isoformat(timespec="seconds"), protocolo),
    )
    if not linhas:
        return jsonify({"erro": "esse chamado já tinha sido alterado por outra ação"}), 409

    try:
        telegram_api("sendMessage", chat_id=ticket["user_id"], text=f"✅ Atendimento {protocolo} finalizado pelo COP.")
    except Exception:
        pass

    if ticket.get("message_thread_id") and ticket.get("grupo_atendente"):
        try:
            telegram_api("closeForumTopic", chat_id=ticket["grupo_atendente"], message_thread_id=ticket["message_thread_id"])
        except Exception:
            pass

    return jsonify({"ok": True})


@app.route("/api/dashboard")
@app.route("/api/dashboard/<int:dias>")
def api_dashboard(dias=1):
    dias = max(1, min(dias, 90))  # limite de segurança

    # created_at/closed_at são gravados como TEXTO em formato ISO pelo bot
    # ("AAAA-MM-DDTHH:MM:SS"). Nesse formato, comparação de texto já é
    # equivalente a comparação cronológica, então calculamos o corte em
    # Python (mais fácil de revisar do que depender de sintaxe de INTERVAL
    # do Postgres) em vez de usar CURRENT_DATE fixo.
    corte = (agora() - timedelta(days=dias - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat(timespec="seconds")

    resumo = {
        "aguardando": fetchone("SELECT COUNT(*) c FROM tickets WHERE status='aguardando'")["c"],
        "em_atendimento": fetchone("SELECT COUNT(*) c FROM tickets WHERE status='em_atendimento'")["c"],
        "finalizados_hoje": fetchone("""
            SELECT COUNT(*) c FROM tickets
            WHERE status='finalizado'
            AND closed_at >= %s
        """, (corte,))["c"],
        "total_hoje": fetchone("""
            SELECT COUNT(*) c FROM tickets
            WHERE created_at >= %s
        """, (corte,))["c"],
    }

    # "Aguardando" e "Em atendimento" mostram o estado ATUAL (agora mesmo),
    # então continuam sem filtro de data — não faria sentido dizer "o que
    # está esperando nos últimos 7 dias", só existe "o que está esperando
    # agora".
    aguardando = fetchall("""
        SELECT protocolo, user_name, categoria, subcategoria, contrato, created_at, fotos
        FROM tickets
        WHERE status='aguardando'
        ORDER BY id ASC
        LIMIT 100
    """)

    em_atendimento = fetchall("""
        SELECT protocolo, user_name, categoria, subcategoria, contrato, atendente_nome, assumed_at, created_at, fotos
        FROM tickets
        WHERE status='em_atendimento'
        ORDER BY assumed_at ASC
        LIMIT 100
    """)

    finalizados = fetchall("""
        SELECT protocolo, user_name, categoria, subcategoria, contrato, atendente_nome, created_at, assumed_at, closed_at
        FROM tickets
        WHERE status='finalizado'
        AND closed_at >= %s
        ORDER BY closed_at DESC
        LIMIT 200
    """, (corte,))

    ranking = fetchall("""
        SELECT atendente_nome, COUNT(*) total
        FROM tickets
        WHERE status='finalizado'
        AND atendente_nome IS NOT NULL
        AND closed_at >= %s
        GROUP BY atendente_nome
        ORDER BY total DESC
        LIMIT 20
    """, (corte,))

    por_fila = fetchall("""
        SELECT categoria, COUNT(*) total
        FROM tickets
        WHERE created_at >= %s
        GROUP BY categoria
        ORDER BY total DESC
    """, (corte,))

    ultimos = fetchall("""
        SELECT protocolo, user_name, categoria, subcategoria, status, atendente_nome, created_at, closed_at
        FROM tickets
        ORDER BY id DESC
        LIMIT 50
    """)

    ag = []
    for r in aguardando:
        x = dict(r)
        x["espera_min"] = minutos(x.get("created_at"))
        ag.append(x)

    at = []
    for r in em_atendimento:
        x = dict(r)
        x["tempo_atendimento_min"] = minutos(x.get("assumed_at"))
        x["tempo_total_min"] = minutos(x.get("created_at"))
        at.append(x)

    fin = []
    for r in finalizados:
        x = dict(r)
        if x.get("assumed_at") and x.get("closed_at"):
            try:
                a = datetime.fromisoformat(str(x["assumed_at"]))
                c = datetime.fromisoformat(str(x["closed_at"]))
                x["duracao_min"] = int((c - a).total_seconds() // 60)
            except Exception:
                x["duracao_min"] = "-"
        else:
            x["duracao_min"] = "-"
        fin.append(x)

    resumo["maior_espera"] = max([x["espera_min"] for x in ag] or [0])

    return jsonify({
        "resumo": resumo,
        "aguardando": ag,
        "em_atendimento": at,
        "finalizados": fin,
        "ranking": [dict(x) for x in ranking],
        "por_fila": [dict(x) for x in por_fila],
        "ultimos": [dict(x) for x in ultimos],
        "periodo_dias": dias,
        "atualizado_em": agora().strftime("%H:%M:%S")
    })


@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": []})

    like = f"%{q}%"
    rows = fetchall("""
        SELECT protocolo, user_name, categoria, subcategoria, contrato, status, atendente_nome, created_at, closed_at
        FROM tickets
        WHERE protocolo ILIKE %s
           OR contrato ILIKE %s
           OR user_name ILIKE %s
           OR categoria ILIKE %s
           OR COALESCE(subcategoria, '') ILIKE %s
           OR COALESCE(atendente_nome, '') ILIKE %s
        ORDER BY id DESC
        LIMIT 30
    """, (like, like, like, like, like, like))

    return jsonify({"results": [dict(r) for r in rows]})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
