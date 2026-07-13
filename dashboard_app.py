import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from flask import Flask, jsonify, render_template, request

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("Configure DATABASE_URL no Railway.")

app = Flask(__name__)

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


@app.route("/")
def index():
    return render_template("dashboard.html")


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
