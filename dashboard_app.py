import os
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from flask import Flask, jsonify, render_template, request

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("Configure DATABASE_URL no Railway.")

app = Flask(__name__)


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def minutos(dt_iso):
    if not dt_iso:
        return 0
    try:
        dt = datetime.fromisoformat(str(dt_iso))
        return int((datetime.now() - dt).total_seconds() // 60)
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
def api_dashboard():
    resumo = {
        "aguardando": fetchone("SELECT COUNT(*) c FROM tickets WHERE status='aguardando'")["c"],
        "em_atendimento": fetchone("SELECT COUNT(*) c FROM tickets WHERE status='em_atendimento'")["c"],
        "finalizados_hoje": fetchone("""
            SELECT COUNT(*) c FROM tickets
            WHERE status='finalizado'
            AND DATE(closed_at::timestamp)=CURRENT_DATE
        """)["c"],
        "total_hoje": fetchone("""
            SELECT COUNT(*) c FROM tickets
            WHERE DATE(created_at::timestamp)=CURRENT_DATE
        """)["c"],
    }

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
        AND DATE(closed_at::timestamp)=CURRENT_DATE
        ORDER BY closed_at DESC
        LIMIT 50
    """)

    ranking = fetchall("""
        SELECT atendente_nome, COUNT(*) total
        FROM tickets
        WHERE status='finalizado'
        AND atendente_nome IS NOT NULL
        AND DATE(closed_at::timestamp)=CURRENT_DATE
        GROUP BY atendente_nome
        ORDER BY total DESC
        LIMIT 20
    """)

    por_fila = fetchall("""
        SELECT categoria, COUNT(*) total
        FROM tickets
        WHERE DATE(created_at::timestamp)=CURRENT_DATE
        GROUP BY categoria
        ORDER BY total DESC
    """)

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
        "atualizado_em": datetime.now().strftime("%H:%M:%S")
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
